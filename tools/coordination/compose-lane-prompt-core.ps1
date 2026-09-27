<#
.SYNOPSIS
    Pure composition logic for turning a fields-*.md or a full card-*.md into a dispatch-ready
    lane prompt. DOT-SOURCED by Compose-LanePrompt.ps1 (the tracked, hashed CLI) and by
    Invoke-Workstream.ps1 (which needs the resolved branch name, not just prompt text, before it
    can create the lane's worktree).

.DESCRIPTION
    THE COMPOSER CONTRACT lives in product-card-TEMPLATE.md's own trailing HTML comment (the
    ratified manifest surface) - this file implements it, it does not restate it. Two shapes:

      FIELDS FILE (basename starts with 'fields-'): the labeled fields it carries are substituted
      into product-card-TEMPLATE.md. Any line matching ^[A-Z][A-Z0-9_]*: at column 0 whose label is
      not in the known set is an UNKNOWN FIELD and composition is REFUSED - nothing is silently
      dropped.

      FULL CARD (anything else, e.g. card-*.md): the prose is already complete; only the runtime
      placeholders the card actually carries are substituted (WORKDIR, BASE_SHA, RUNDIR, TS, BRANCH,
      PR_STEP). A full card with no {{BRANCH}} token names its branch as a literal string instead
      (`git switch -c <literal>`), which this file extracts so the caller's worktree gets the same
      branch the composed prompt tells the lane to switch to.

    PR_STEP has exactly two ratified literals (plan 0.35): one for ghCapability=lane-can-open-pr,
    one for everything else. Both carry a nested {{BRANCH}}, which is MATERIALISED before PR_STEP is
    inserted into the template - so by the time the general substitution pass runs, PR_STEP is
    already a closed string and one pass is sufficient (equivalent to the contract's "fixed point"
    at convergence).

    NO Set-StrictMode HERE. landing-probe.ps1 already paid for this lesson: a dot-sourced helper
    that turns on strict mode changes its CALLER's semantics just by being consulted.
#>

$script:KnownFieldLabels = @(
    'CARD_ID', 'PRIORITY', 'CLIP_OR_NONE', 'ALLOWED_PATHS', 'BRANCH',
    'STATE', 'DEPENDS_ON', 'NOTE', 'DELIVERABLE', 'ACCEPTANCE', 'VERIFY_FIRST'
)

# Both literals pinned by plan 0.35 / O153 / S122, verbatim from product-card-TEMPLATE.md's
# COMPOSER CONTRACT comment. Never retype these from memory - re-derive from the template.
$script:PrStepLaneCanOpenPr = 'gh pr create -R layibabalola/MLV-App --head {{BRANCH}} --title "<card id>: <subject>" --body "<what, why, red run, green run>"; then print PR-OPENED: <number> as your last line.'
$script:PrStepOtherwise = 'Do NOT call gh. Print PUSHED: {{BRANCH}} <head sha> as your last line; the dispatcher opens the PR.'

function Get-PrStepLiteral {
    param([string]$GhCapability, [string]$Branch)
    $raw = if ($GhCapability -eq 'lane-can-open-pr') { $script:PrStepLaneCanOpenPr } else { $script:PrStepOtherwise }
    return $raw.Replace('{{BRANCH}}', $Branch)
}

function Get-ParsedFields {
    # Splits a fields-*.md file into a label => value map. A label starts a new field at the value
    # on the SAME line (if any) plus every following line up to (not including) the next label line
    # or EOF, with leading/trailing blank lines trimmed off that block - so "ALLOWED_PATHS: ...\n\n"
    # does not carry the trailing blank line into the field's value.
    param([string[]]$Lines)

    $fieldMap = @{}
    $current = $null
    $buffer = New-Object System.Collections.Generic.List[string]

    foreach ($line in $Lines) {
        if ($line -cmatch '^([A-Z][A-Z0-9_]*):(.*)$') {
            if ($null -ne $current) {
                while ($buffer.Count -gt 0 -and $buffer[0].Trim() -eq '') { $buffer.RemoveAt(0) }
                while ($buffer.Count -gt 0 -and $buffer[$buffer.Count - 1].Trim() -eq '') { $buffer.RemoveAt($buffer.Count - 1) }
                $fieldMap[$current] = ($buffer -join "`n")
            }
            $label = $Matches[1]
            if ($script:KnownFieldLabels -notcontains $label) {
                throw "unknown-field: $label"
            }
            $current = $label
            $buffer = New-Object System.Collections.Generic.List[string]
            $rest = $Matches[2]
            if ($rest.StartsWith(' ')) { $rest = $rest.Substring(1) }
            if ($rest.Trim() -ne '') { [void]$buffer.Add($rest) }
        } elseif ($null -ne $current) {
            [void]$buffer.Add($line)
        }
        # A line before the first label (the file's own "# FIELDS for ..." comment) is ignored.
    }
    if ($null -ne $current) {
        while ($buffer.Count -gt 0 -and $buffer[0].Trim() -eq '') { $buffer.RemoveAt(0) }
        while ($buffer.Count -gt 0 -and $buffer[$buffer.Count - 1].Trim() -eq '') { $buffer.RemoveAt($buffer.Count - 1) }
        $fieldMap[$current] = ($buffer -join "`n")
    }
    return $fieldMap
}


function Get-DoctrineBriefText {
    # Fail-closed: implementer/editing composition requires a brief. Fixture via
    # MLV_DOCTRINE_FIXTURE_ROOT for offline tests ONLY: get_doctrine_brief.py refuses it
    # unless PYTEST_CURRENT_TEST is set and the root sits in tools/coordination/fixtures/.
    # Never browses the bus from a lane;
    # this runs on the hub/dispatcher host only.
    param(
        [string]$FixtureRoot = ''
    )
    $getter = Join-Path $PSScriptRoot 'Get-DoctrineBrief.ps1'
    if (-not (Test-Path -LiteralPath $getter)) {
        throw "doctrine-brief-missing: $getter"
    }
    # HASHTABLE splat, not an array: an array splat of '-FixtureRoot',<path> into a script binds
    # both strings POSITIONALLY (to -DoctrineRepo/-Ref), so the fixture never reached the getter
    # and python saw `--repo -FixtureRoot`. (Also never reuse the automatic $args variable.)
    $getterArgs = @{}
    if ($FixtureRoot) {
        $getterArgs['FixtureRoot'] = $FixtureRoot
    } elseif ($env:MLV_DOCTRINE_FIXTURE_ROOT) {
        $getterArgs['FixtureRoot'] = $env:MLV_DOCTRINE_FIXTURE_ROOT
    }
    $output = & $getter @getterArgs 2>&1 | Out-String
    $code = $LASTEXITCODE
    if ($null -eq $code) { $code = 0 }
    $trimmed = if ($null -eq $output) { '' } else { $output.TrimEnd() }
    if ($code -ne 0 -or $trimmed -match '^REFUSED:') {
        $msg = if ($trimmed) { $trimmed } else { "doctrine-brief-failed exit=$code" }
        throw ("doctrine-brief-failed: {0}" -f $msg)
    }
    if (-not $trimmed) {
        throw "doctrine-brief-failed: empty brief"
    }
    # Provenance is part of the brief (get_doctrine_brief.py writes it); a brief without it is
    # not one we produced. Defence in depth behind the getter's own gate: a fixture brief is
    # refused unless pytest is driving this composition. There is deliberately NO parameter
    # for handing in brief text - the only brief a lane prompt can carry is this fetch.
    $provMatch = [regex]::Match($trimmed, '(?m)^provenance: (live|fixture)\s*$')
    if (-not $provMatch.Success) {
        throw "doctrine-brief-failed: brief carries no provenance line"
    }
    if ($provMatch.Groups[1].Value -eq 'fixture' -and -not $env:PYTEST_CURRENT_TEST) {
        throw "doctrine-brief-failed: doctrine-fixture-refused: fixture brief outside pytest"
    }
    return $trimmed
}

function Get-ComposedLanePrompt {
    param(
        [Parameter(Mandatory)][string]$ProcedurePath,
        [Parameter(Mandatory)][string]$TemplatePath,
        [Parameter(Mandatory)][string]$WorkDir,
        [Parameter(Mandatory)][string]$BaseSha,
        [Parameter(Mandatory)][string]$RunDir,
        [string]$Ts = '',
        [Parameter(Mandatory)][string]$GhCapability,
        [string]$DoctrineFixtureRoot = '',
        [switch]$SkipDoctrineBrief
    )
    if (-not (Test-Path -LiteralPath $ProcedurePath)) { throw "procedure-missing: $ProcedurePath" }
    $procedureText = Get-Content -LiteralPath $ProcedurePath -Raw
    $basename = Split-Path -Leaf $ProcedurePath
    $isFieldsFile = $basename -like 'fields-*'

    if ($isFieldsFile) {
        if (-not (Test-Path -LiteralPath $TemplatePath)) { throw "template-missing: $TemplatePath" }
        $templateText = Get-Content -LiteralPath $TemplatePath -Raw
        $lines = $procedureText -split "`r?`n"
        $fields = Get-ParsedFields -Lines $lines

        $cardId = [string]$fields['CARD_ID']
        $branch = if ($fields.ContainsKey('BRANCH') -and $fields['BRANCH']) { [string]$fields['BRANCH'] } else { "product/$cardId" }
        $state = if ($fields.ContainsKey('STATE') -and $fields['STATE']) { [string]$fields['STATE'] } else { 'ACTIVE' }
        $dependsOn = if ($fields.ContainsKey('DEPENDS_ON') -and $fields['DEPENDS_ON']) { [string]$fields['DEPENDS_ON'] } else { 'none' }
        $note = if ($fields.ContainsKey('NOTE') -and $fields['NOTE']) { [string]$fields['NOTE'] } else { 'none' }

        $prStep = Get-PrStepLiteral -GhCapability $GhCapability -Branch $branch

        $composed = $templateText
        $composed = $composed.Replace('{{PR_STEP}}', $prStep)
        $composed = $composed.Replace('{{CARD_ID}}', $cardId)
        $composed = $composed.Replace('{{PRIORITY}}', [string]$fields['PRIORITY'])
        $composed = $composed.Replace('{{CLIP_OR_NONE}}', [string]$fields['CLIP_OR_NONE'])
        $composed = $composed.Replace('{{ALLOWED_PATHS}}', [string]$fields['ALLOWED_PATHS'])
        $composed = $composed.Replace('{{STATE}}', $state)
        $composed = $composed.Replace('{{DEPENDS_ON}}', $dependsOn)
        $composed = $composed.Replace('{{NOTE}}', $note)
        $composed = $composed.Replace('{{DELIVERABLE}}', [string]$fields['DELIVERABLE'])
        $composed = $composed.Replace('{{ACCEPTANCE}}', [string]$fields['ACCEPTANCE'])
        $composed = $composed.Replace('{{VERIFY_FIRST}}', [string]$fields['VERIFY_FIRST'])
        $composed = $composed.Replace('{{BRANCH}}', $branch)
        $composed = $composed.Replace('{{WORKDIR}}', $WorkDir)
        $composed = $composed.Replace('{{BASE_SHA}}', $BaseSha)
        $composed = $composed.Replace('{{RUNDIR}}', $RunDir)
        $composed = $composed.Replace('{{TS}}', $Ts)

        # Implementer/editing path (fields-* -> product-card-TEMPLATE): doctrine brief is mandatory.
        if ($composed -match '\{\{DOCTRINE_BRIEF\}\}') {
            if ($SkipDoctrineBrief) {
                throw "doctrine-brief-failed: SkipDoctrineBrief is not permitted on implementer/editing paths"
            }
            $brief = Get-DoctrineBriefText -FixtureRoot $DoctrineFixtureRoot
            $composed = $composed.Replace('{{DOCTRINE_BRIEF}}', $brief)
        }

        if ($composed -match '\{\{[A-Z_]+\}\}') {
            throw "composer-incomplete: unresolved placeholder $($Matches[0]) in $cardId"
        }

        return [pscustomobject]@{ Text = $composed; Branch = $branch; CardId = $cardId }
    } else {
        $cardIdMatch = [regex]::Match($procedureText, '(?m)^#\s*(?:PRODUCT )?CARD:\s*(\S+)')
        $cardId = if ($cardIdMatch.Success) { $cardIdMatch.Groups[1].Value } else { [IO.Path]::GetFileNameWithoutExtension($ProcedurePath) }

        $branch = "product/$cardId"
        $branchLiteralMatch = [regex]::Match($procedureText, '(?m)git switch -c ([^\s`]+)')
        if ($branchLiteralMatch.Success -and $branchLiteralMatch.Groups[1].Value -ne '{{BRANCH}}') {
            $branch = $branchLiteralMatch.Groups[1].Value
        }

        $prStep = Get-PrStepLiteral -GhCapability $GhCapability -Branch $branch

        $composed = $procedureText
        $composed = $composed.Replace('{{PR_STEP}}', $prStep)
        $composed = $composed.Replace('{{WORKDIR}}', $WorkDir)
        $composed = $composed.Replace('{{BASE_SHA}}', $BaseSha)
        $composed = $composed.Replace('{{RUNDIR}}', $RunDir)
        $composed = $composed.Replace('{{TS}}', $Ts)
        $composed = $composed.Replace('{{BRANCH}}', $branch)

        if ($composed -match '\{\{DOCTRINE_BRIEF\}\}') {
            # Full card carrying the placeholder is an editing/implementer path: refuse if brief fails.
            if ($SkipDoctrineBrief) {
                throw "doctrine-brief-failed: SkipDoctrineBrief is not permitted when {{DOCTRINE_BRIEF}} is present"
            }
            $brief = Get-DoctrineBriefText -FixtureRoot $DoctrineFixtureRoot
            $composed = $composed.Replace('{{DOCTRINE_BRIEF}}', $brief)
        }

        if ($composed -match '\{\{[A-Z_]+\}\}') {
            throw "composer-incomplete: unresolved placeholder $($Matches[0]) in $ProcedurePath"
        }

        return [pscustomobject]@{ Text = $composed; Branch = $branch; CardId = $cardId }
    }
}
