# attr3_footage_io_inventory_scan.ps1 -- ATTR3-FOOTAGE-STAGE-1 round 7 (item 3: pinned I/O
# inventory). Enumerates every file-system cmdlet call and .NET I/O member call in the four
# ATTR3-FOOTAGE-STAGE-1-owned files -- attr3-footage-stage.ps1, Attr3FootageStageJob.psm1,
# Attr3FootagePresenceJob.psm1, AttrCudaOwnerFootage.psm1 -- INCLUDING the two emitted job
# templates ($template here-strings in the two *Job.psm1 files), parsed as their own scripts too.
#
# WHY THIS EXISTS. Every round since round 3 found a NEW instance of the same two recurring
# defect classes (link containment at some I/O site; a path escaping through an uncaught
# exception) because nothing PROVED the set of I/O call sites was finite and fully reviewed --
# each round's fix covered only the specific site a review happened to find. This scan makes the
# set of I/O call sites literally enumerable and diffable: test_attr3_footage_io_inventory.py
# compares this scan's live output against a checked-in, hand-classified inventory
# (attr3_footage_io_inventory.json) and fails on any row that is NEW, CHANGED (moved line, changed
# text) or MISSING a disposition -- forcing every future edit that touches an I/O call in these
# four files through an explicit re-classification, not just a review that might miss it.
#
# SCOPE. AttrCudaArtifacts.psm1 (the shared verifier module every PLAYBACK-ATTR-3-CUDA job
# embeds, including these two) is deliberately OUT OF SCOPE -- it belongs to a much larger family
# of job templates than just ATTR3-FOOTAGE-STAGE-1's own four files, and folding it in here would
# make the inventory unbounded rather than finite. The two job templates are scanned as their OWN
# authored text (placeholders neutralised, exactly as tools/repo_hygiene/attr3_publish_write_
# scan.ps1 already does for the playback-attr-3-cuda template family) -- the embedded verifier
# FUNCTION BODIES spliced in from AttrCudaArtifacts.psm1 at generation time are not re-scanned
# here, since their own source already lives, and is reviewed, elsewhere.
#
# WHAT COUNTS AS AN I/O CALL. A fixed, small allowlist -- not "every cmdlet" or "every member
# call" (that would drown a handful of real I/O sites in thousands of unrelated hits: .Add(),
# .Count, .ToString(), and so on, none of which touch the filesystem). FS_CMDLETS covers the
# filesystem cmdlets these files actually use; FS_METHODS covers the .NET I/O method NAMES they
# actually call (static, e.g. [IO.File]::Open, and instance, e.g. $stream.Dispose()) -- matched by
# name only, not by receiver type (the same simplification attr3_publish_write_scan.ps1's own R4
# already makes, for the same reason: PowerShell's AST does not carry static type information for
# an arbitrary variable's runtime type).
#
# Output: JSON array of rows { source, function, line, kind, name, text }.

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Bachelor = Join-Path $PSScriptRoot '..\profiling\bachelor'
$Sources = [ordered]@{
    'attr3-footage-stage.ps1'                       = (Join-Path $Bachelor 'attr3-footage-stage.ps1')
    'Attr3FootageStageJob.psm1'                     = (Join-Path $Bachelor 'Attr3FootageStageJob.psm1')
    'Attr3FootagePresenceJob.psm1'                  = (Join-Path $Bachelor 'Attr3FootagePresenceJob.psm1')
    'AttrCudaOwnerFootage.psm1'                      = (Join-Path $Bachelor 'AttrCudaOwnerFootage.psm1')
}
# Job-builder modules whose own $template here-string is ALSO scanned as its own source, under a
# '<file>::template' label -- see this script's own header on why (the emitted job runs on a host
# with no checkout; its own logic must be enumerated independently of the generator that writes it).
$TemplateHosts = @('Attr3FootageStageJob.psm1', 'Attr3FootagePresenceJob.psm1')

$FsCmdlets = @(
    'Get-ChildItem', 'Get-Item', 'Test-Path', 'Remove-Item', 'New-Item', 'Copy-Item', 'Move-Item',
    'Get-Content', 'Set-Content', 'Get-FileHash', 'Resolve-Path', 'Rename-Item'
)
$FsMethods = @(
    'Open', 'Dispose', 'Close', 'CopyTo', 'Flush', 'Write', 'WriteByte', 'Move', 'Delete', 'Create',
    'ReadAllBytes', 'ReadAllText', 'WriteAllText', 'WriteAllBytes',
    'CreateFileW', 'GetFileInformationByHandle', 'CloseHandle'
)
# Static calls: only against a fixed, small set of I/O types -- otherwise a member NAME this
# narrow (Open/Create/Move/...) still false-positives on an unrelated type (e.g.
# [Security.Cryptography.SHA256]::Create()) that happens to share the method name.
$IoStaticTypes = @('IO.File', 'IO.Directory', 'AttrCudaWin32.NativeMethods')
# Instance calls: PowerShell's AST carries no runtime type for an arbitrary variable, so the same
# name-only ambiguity applies (e.g. $sha256Alg.Dispose() is a hash algorithm, not a stream). Every
# real stream/handle variable in these four files is named *Stream or exactly $handle -- an
# established convention, not a guess -- so an instance call is only counted when its receiver is
# a plain variable matching that pattern, OR is not a plain variable at all (a shape these files
# do not currently use; kept IN rather than silently excluded, so a future one still surfaces).
$IoInstanceReceiverPattern = '(?i)stream|handle'

function Get-EnclosingFunctionName($Node) {
    $cursor = $Node.Parent
    while ($null -ne $cursor) {
        if ($cursor -is [System.Management.Automation.Language.FunctionDefinitionAst]) { return $cursor.Name }
        $cursor = $cursor.Parent
    }
    return '<top-level>'
}

function Get-ShortText($Ast) {
    $text = ($Ast.Extent.Text -replace '\s+', ' ').Trim()
    if ($text.Length -gt 120) { $text = $text.Substring(0, 120) }
    return $text
}

function Invoke-Attr3IoScan([string]$SourceLabel, [string]$Text) {
    $rows = [System.Collections.Generic.List[object]]::new()
    $tokens = $null
    $parseErrors = $null
    $root = [System.Management.Automation.Language.Parser]::ParseInput($Text, [ref]$tokens, [ref]$parseErrors)
    foreach ($err in @($parseErrors)) {
        $rows.Add([ordered]@{
            source = $SourceLabel; function = '<parse-error>'; line = $err.Extent.StartLineNumber
            kind = 'parse-error'; name = ''; text = $err.Message
        })
    }

    foreach ($command in $root.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)) {
        $name = $command.GetCommandName()
        if ([string]::IsNullOrEmpty($name)) { continue }
        if ($FsCmdlets -cnotcontains $name) { continue }
        $rows.Add([ordered]@{
            source = $SourceLabel; function = (Get-EnclosingFunctionName $command); line = $command.Extent.StartLineNumber
            kind = 'cmdlet'; name = $name; text = (Get-ShortText $command)
        })
    }

    foreach ($member in $root.FindAll({ param($n) $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }, $true)) {
        if ($member.Member -isnot [System.Management.Automation.Language.StringConstantExpressionAst]) { continue }
        $memberName = [string]$member.Member.Value
        if ($FsMethods -cnotcontains $memberName) { continue }
        if ($member.Static) {
            $typeText = if ($member.Expression -is [System.Management.Automation.Language.TypeExpressionAst]) {
                $member.Expression.TypeName.FullName
            } else {
                $null
            }
            if ($null -eq $typeText -or $IoStaticTypes -cnotcontains $typeText) { continue }
        } else {
            $isPlainVariable = $member.Expression -is [System.Management.Automation.Language.VariableExpressionAst]
            if ($isPlainVariable -and $member.Expression.VariablePath.UserPath -notmatch $IoInstanceReceiverPattern) { continue }
        }
        $kind = if ($member.Static) { 'static-method' } else { 'instance-method' }
        $rows.Add([ordered]@{
            source = $SourceLabel; function = (Get-EnclosingFunctionName $member); line = $member.Extent.StartLineNumber
            kind = $kind; name = $memberName; text = (Get-ShortText $member)
        })
    }

    return , $rows
}

$allRows = [System.Collections.Generic.List[object]]::new()
foreach ($label in $Sources.Keys) {
    $path = $Sources[$label]
    $text = [IO.File]::ReadAllText($path)
    foreach ($row in (Invoke-Attr3IoScan $label $text)) { $allRows.Add($row) }

    if ($TemplateHosts -contains $label) {
        $match = [regex]::Match($text, "(?s)\`$template = @'\r?\n(.*?)\r?\n'@")
        if (-not $match.Success) {
            $allRows.Add([ordered]@{
                source = "$label::template"; function = '<parse-error>'; line = 0
                kind = 'template-missing'; name = ''; text = "no `$template here-string found in $label"
            })
        } else {
            $templateText = [regex]::Replace($match.Groups[1].Value, '__[A-Z0-9_]+__', '$attrFootageIoPlaceholder')
            foreach ($row in (Invoke-Attr3IoScan "$label::template" $templateText)) { $allRows.Add($row) }
        }
    }
}

@($allRows | Sort-Object source, line) | ConvertTo-Json -Depth 6
