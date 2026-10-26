# Summarize-VenueBusyProbes.ps1 -- READ-ONLY: which projects held the CPU in the busy quiet probes (VENUE-PROBE-PROCESS-ATTRIBUTION-1). Run it where the probes' raw files are.
#
# Wait-VenueQuiet.ps1 retains each probe's VENUE_QUIET line verbatim as <WorkDir>\venue-quiet-probe-<utc>.json. A probe that carries `identity`
# (mlv-app/venue-quiet-probe-identity/v1) names WHO started each top-5 CPU process (owner, pinned-table project, script, parent chain) and the GPU-heavy pids. This script
# folds those files into the lines a leg report needs:
#   - Busy probes summarized: <n> (quiet skipped <q>, unknown skipped <u>, unreadable <r>)
#   - Busy probes by project (probes in which the project held a top-5 CPU row; cpuSeconds summed over those rows): <project> (<n> probes, <s> s; e.g. <name> script=<s> owner=<o>), ...
#   - GPU-heavy across busy probes: <name> pid=<n> project=<p> max gpu=<x.x%> (<n> probes)   |   none recorded
#
#   pwsh -NoProfile -File "<repo>\tools\profiling\dual-venue\Summarize-VenueBusyProbes.ps1" -ProbeDir <work dir> [-ThresholdPercent 20] [-IncludeQuiet] [-Json]
#   pwsh -NoProfile -File "<repo>\tools\profiling\dual-venue\Summarize-VenueBusyProbes.ps1" -ProbeJson <file>,<file>
# -ProbeJson takes a comma-joined list (under `pwsh -File` a list reaches a [string[]] as ONE string, so it is split here; a path that exists as given is never split, so a comma
# in a file name is safe). A probe is identified by its normalised full path: a file named twice (two spellings, or by -ProbeJson and again by -ProbeDir) counts once, and the
# same file name in two directories counts as two probes.
#
# THE DECISION IS THE GATE'S OWN. Get-VenueQuietDecision is LIFTED from Wait-VenueQuiet.ps1 by AST (there is no second copy of the quiet rule); when it cannot be lifted this
# exits 3 DECISION_LIFT_FAILED. QUIET and UNKNOWN probes are skipped (and counted) unless -IncludeQuiet keeps the QUIET ones. A file that does not parse is counted `unreadable`.
# A probe with no identity (v1, v2), or a top row with no identity row, is grouped under `unknown(no-identity)`; a row whose pid was reused since sampling (start_check
# mismatch) under `unknown(pid-reused)`; a real identity that matches no project under `unknown`. -ThresholdPercent is the quiet threshold the lifted decision uses (default 20)
# and also the GPU-heavy bar: a pid counts as GPU-heavy in a probe when its gpu_percent reached it.
# Every value written on a text line has whitespace and commas replaced by '_'. No command line is ever printed (the files hold one with no argument text; this script never reads it),
# and a script value that is not path-shaped (whitespace, '=', '@', '?', '#', a quote, a ':' past a drive letter) prints as n/a.
#
# READ-ONLY and share-safe: it opens only the files it is given (-ProbeDir: only venue-quiet-probe-*.json directly in that directory, which are the VM-side raw-retention files),
# never lists a venue share, and refuses with exit 2 PROBE_PATH_IN_VENUE_SHARE any path under a venue's agentShare / agentRoot in venues.json.
# Every input (each -ProbeJson entry as given and its comma parts, and -ProbeDir) is judged as TEXT before any Test-Path, Get-Item or read: a UNC (\\host\...) or provider-qualified
# (FileSystem::...) path is refused PROBE_PATH_UNC, so a hung or dead remote share is never touched.
# -PerCoreEnforce / -PerCoreThresholdPercent (VENUE-QUIET-PER-CORE-VERDICT-1) mirror the gate's switches of the same name: with -PerCoreEnforce a mean-QUIET probe whose busiest fast core
# is at or over the threshold counts as BUSY here exactly as the gate decided it, by the gate's own Get-VenuePerCoreVerdict (lifted by AST, no second copy; DECISION_LIFT_FAILED when it
# cannot be). Off (the default) the mean rule alone decides, as before. The retained raw file does not record which switch the gate ran with, so the caller passes the one it used.
# Exit codes: 0 summarized; 2 usage error, PROBE_PATH_UNC or PROBE_PATH_IN_VENUE_SHARE; 3 DECISION_LIFT_FAILED.
[CmdletBinding()]
param(
    [string[]]$ProbeJson = @(),
    [string]$ProbeDir = '',
    [double]$ThresholdPercent = 20.0,
    [ValidateRange(0.0, 100.0)][double]$PerCoreThresholdPercent = 60.0,
    [switch]$PerCoreEnforce,
    [switch]$IncludeQuiet,
    [switch]$Json,
    [string]$DecisionSource = ''
)
$ErrorActionPreference = 'Stop'
# (-ProbeJson is split in step 1b, only after every spelling has been judged as text)
$here = $PSScriptRoot
$identitySchemaV1 = 'mlv-app/venue-quiet-probe-identity/v1'
if (-not $DecisionSource) { $DecisionSource = Join-Path $here 'Wait-VenueQuiet.ps1' }

function Stop-Usage([string]$Message, [int]$Code = 2) { [Console]::Error.WriteLine($Message); exit $Code }
function ConvertTo-PathKey([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { return '' }
    $full = try { [IO.Path]::GetFullPath($Path) } catch { $Path }
    $full.TrimEnd([char[]]@('\', '/')).ToLowerInvariant()
}
function Test-UnderOrEqual([string]$Key, [string]$Root) { $Root -ne '' -and ($Key -ceq $Root -or $Key.StartsWith($Root + '\', [StringComparison]::Ordinal)) }
function ConvertTo-IdentToken($v) { $s = [string]$v; if ([string]::IsNullOrEmpty($s)) { return 'n/a' }; $s -replace '[\s,]', '_' }
# r5: a probe writes `script` only as an allowlisted path, but a raw file from an older probe may hold argument text there, so a script value with whitespace, '=', '@', '?',
# '#', a quote or a ':' past a drive letter is printed as n/a (text only: nothing is resolved or opened)
function Format-IdentScript($v) { $s = [string]$v; if ($s -notmatch '^(?:[A-Za-z]:)?[^\s=@?#:"'']+$') { return 'n/a' }; ConvertTo-IdentToken $s }
function Format-Num([double]$v, [string]$Fmt) { $v.ToString($Fmt, [Globalization.CultureInfo]::InvariantCulture) }

# 0. every spelling of every input -- the entry as given AND its comma parts -- is judged as TEXT first, so a forbidden path is never tested, resolved, opened or listed by a later step
$spellings = @()
foreach ($raw in (@($ProbeJson) + @($ProbeDir))) {
    if ([string]::IsNullOrWhiteSpace([string]$raw)) { continue }
    $spellings += ([string]$raw).Trim()
    $spellings += @(([string]$raw) -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}
foreach ($p in $spellings) {
    if ($p -match '^[\\/]{2}' -or $p.Contains('::')) { Stop-Usage "PROBE_PATH_UNC '$p' is a UNC or provider-qualified path; copy the raw probe files to a local directory first (this script never touches a remote or provider path)" }
}

# 1. refuse venue-share paths BEFORE anything is read or listed
$shareKeys = @()
try {
    $table = [IO.File]::ReadAllText((Join-Path $here 'venues.json')) | ConvertFrom-Json
    foreach ($v in $table.venues.PSObject.Properties) {
        foreach ($p in @($v.Value.agentShare, $v.Value.agentRoot)) { $k = ConvertTo-PathKey ([string]$p); if ($k) { $shareKeys += $k } }
    }
} catch { Stop-Usage "USAGE venues.json could not be read from '$here': $($_.Exception.Message)" }
foreach ($p in $spellings) {
    if (-not $p) { continue }
    $pk = ConvertTo-PathKey $p
    foreach ($k in $shareKeys) {
        if (Test-UnderOrEqual $pk $k) { Stop-Usage "PROBE_PATH_IN_VENUE_SHARE '$p' is inside a venue's agent storage; copy the raw probe files out first (this script never lists or reads a venue share)" }
    }
}

# 1b. only now, with every spelling cleared, may the filesystem be asked: a path that exists as given is never split
$ProbeJson = @($ProbeJson | ForEach-Object { if (Test-Path -LiteralPath $_) { $_ } else { $_ -split ',' } } | ForEach-Object { $_.Trim() } | Where-Object { $_ })   # -File passes 'a','b' as one comma-joined string
if ($ProbeJson.Count -eq 0 -and -not $ProbeDir) { Stop-Usage 'USAGE give -ProbeJson <path[]> or -ProbeDir <dir>' }

# 2. the gate's decision function, lifted by AST
$lifted = $false
try {
    $tokens = $null; $errs = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($DecisionSource, [ref]$tokens, [ref]$errs)
    $fn = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq 'Get-VenueQuietDecision' }, $true))
    if ($fn.Count -eq 1) { . ([scriptblock]::Create($fn[0].Extent.Text)); $lifted = [bool](Get-Command Get-VenueQuietDecision -CommandType Function -ErrorAction SilentlyContinue) }
    if ($lifted -and $PerCoreEnforce) {
        foreach ($name in 'Test-PerCoreNumber', 'Get-VenuePerCoreVerdict') {
            $f = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $name }, $true))
            if ($f.Count -ne 1) { $lifted = $false; break }
            . ([scriptblock]::Create($f[0].Extent.Text))
        }
        if ($lifted) { $lifted = [bool](Get-Command Get-VenuePerCoreVerdict -CommandType Function -ErrorAction SilentlyContinue) }
    }
} catch { $lifted = $false }
if (-not $lifted) { Stop-Usage "DECISION_LIFT_FAILED Get-VenueQuietDecision$(if ($PerCoreEnforce) { ' / Get-VenuePerCoreVerdict' }) could not be lifted from '$DecisionSource'; there is no second copy of the quiet rule" 3 }

# 3. the files
$candidates = @()
foreach ($p in $ProbeJson) { if ($p) { $candidates += $p } }
if ($ProbeDir) {
    if (-not (Test-Path -LiteralPath $ProbeDir -PathType Container)) { Stop-Usage "USAGE -ProbeDir '$ProbeDir' is not a directory" }
    $candidates += @(Get-ChildItem -LiteralPath $ProbeDir -Filter 'venue-quiet-probe-*.json' -File | Sort-Object Name | ForEach-Object { $_.FullName })
}
# one probe = one normalised full path: a file named twice (two spellings, or by -ProbeJson and by -ProbeDir) is read once, and the key that counts a probe per project is that
# path, never the basename (the same name in two directories is two probes)
$files = @(); $fileKey = @{}; $seenKeys = @{}
foreach ($c in $candidates) {
    $k = ConvertTo-PathKey $c
    if ($seenKeys.ContainsKey($k)) { continue }
    $seenKeys[$k] = $true; $files += $c; $fileKey[$c] = $k
}
$unreadable = 0; $quietSkipped = 0; $unknownSkipped = 0; $busy = 0
$byProject = @{}; $gpuHeavy = @{}
foreach ($f in $files) {
    $probe = $null
    try { $probe = [IO.File]::ReadAllText($f) | ConvertFrom-Json; if ($null -eq $probe -or $probe -isnot [pscustomobject]) { throw 'not a JSON object' } } catch { $unreadable++; continue }
    $d = Get-VenueQuietDecision -Samples @($probe.samples) -Threshold $ThresholdPercent
    if ($PerCoreEnforce -and $d.state -ceq 'QUIET' -and (Get-VenuePerCoreVerdict $probe $PerCoreThresholdPercent $true).would_block -eq $true) { $d = [pscustomobject]@{ state = 'BUSY'; mean = $d.mean } }
    if ($d.state -ceq 'UNKNOWN') { $unknownSkipped++; continue }
    if ($d.state -ceq 'QUIET' -and -not $IncludeQuiet) { $quietSkipped++; continue }
    $busy++
    $map = $null
    if ([string]$probe.identity.schema -ceq $identitySchemaV1) {
        $map = @{}
        foreach ($r in @($probe.identity.processes)) { if ($null -ne $r -and $null -ne $r.pid) { $map["$($r.pid)"] = $r } }
    }
    $probeKey = $fileKey[$f]
    foreach ($t in @($probe.top)) {
        $row = $(if ($null -ne $map -and $null -ne $t.pid -and $map.ContainsKey("$($t.pid)")) { $map["$($t.pid)"] } else { $null })
        $project = 'unknown(no-identity)'; $script = 'n/a'; $owner = 'n/a'
        if ($null -ne $row) {
            if ([string]$row.start_check -ceq 'mismatch') { $project = 'unknown(pid-reused)' }
            else {
                $project = $(if ([string]$row.project) { [string]$row.project } else { 'unknown' })
                $script = Format-IdentScript $row.script
                $owner = $(if ($row.owner) { ConvertTo-IdentToken $row.owner } else { 'unreadable(' + (ConvertTo-IdentToken $row.owner_status) + ')' })
            }
        }
        $secs = $(if ($null -ne $t.cpuSeconds) { [double]$t.cpuSeconds } else { 0.0 })
        if (-not $byProject.ContainsKey($project)) { $byProject[$project] = @{ probes = @{}; seconds = 0.0; examples = @{} } }
        $e = $byProject[$project]
        $e.probes[$probeKey] = $true; $e.seconds += $secs
        $ex = "$(ConvertTo-IdentToken $t.name)|$script|$owner"
        $e.examples[$ex] = [double]$e.examples[$ex] + $secs
    }
    if ($null -ne $map) {
        foreach ($r in $map.Values) {
            if ([string]$r.start_check -ceq 'mismatch' -or $null -eq $r.gpu_percent) { continue }
            if ([double]$r.gpu_percent -lt $ThresholdPercent) { continue }
            $gk = "$(ConvertTo-IdentToken $r.name)|$($r.pid)|$(ConvertTo-IdentToken $r.project)"
            if (-not $gpuHeavy.ContainsKey($gk)) { $gpuHeavy[$gk] = @{ name = (ConvertTo-IdentToken $r.name); pid = $r.pid; project = (ConvertTo-IdentToken $r.project); max = 0.0; probes = @{} } }
            $g = $gpuHeavy[$gk]
            if ([double]$r.gpu_percent -gt $g.max) { $g.max = [double]$r.gpu_percent }
            $g.probes[$probeKey] = $true
        }
    }
}

# 4. the aggregate
$projects = @($byProject.Keys | ForEach-Object {
    $e = $byProject[$_]
    $examples = @($e.examples.Keys | Sort-Object @{ Expression = { $e.examples[$_] }; Descending = $true }, @{ Expression = { $_ } } | Select-Object -First 3 | ForEach-Object {
        $parts = $_ -split '\|', 3
        [pscustomobject]@{ name = $parts[0]; script = $parts[1]; owner = $parts[2] }
    })
    [pscustomobject]@{ project = $_; probes = $e.probes.Count; cpu_seconds = [math]::Round($e.seconds, 2); examples = $examples }
} | Sort-Object @{ Expression = 'cpu_seconds'; Descending = $true }, @{ Expression = 'project' })
$gpu = @($gpuHeavy.Values | Sort-Object @{ Expression = { $_.max }; Descending = $true }, @{ Expression = { $_.name } }, @{ Expression = { [int64]$_.pid } } |
    ForEach-Object { [pscustomobject]@{ name = $_.name; pid = $_.pid; project = $_.project; max_gpu_percent = [math]::Round($_.max, 1); probes = $_.probes.Count } })

if ($Json) {
    [ordered]@{
        schema = 'mlv-app/venue-busy-probe-summary/v1'
        threshold_percent = $ThresholdPercent
        busy_probes = $busy; quiet_skipped = $quietSkipped; unknown_skipped = $unknownSkipped; unreadable = $unreadable
        projects = $projects
        gpu_heavy = $gpu
    } | ConvertTo-Json -Compress -Depth 6
    exit 0
}
Write-Output "- Busy probes summarized: $busy (quiet skipped $quietSkipped, unknown skipped $unknownSkipped, unreadable $unreadable)"
$projText = $(if ($projects.Count -eq 0) { 'none' } else {
    ($projects | ForEach-Object { "{0} ({1} probes, {2} s; e.g. {3})" -f $_.project, $_.probes, (Format-Num $_.cpu_seconds '0.0'), (($_.examples | ForEach-Object { "{0} script={1} owner={2}" -f $_.name, $_.script, $_.owner }) -join '; ') }) -join ', '
})
Write-Output "- Busy probes by project (probes in which the project held a top-5 CPU row; cpuSeconds summed over those rows): $projText"
$gpuText = $(if ($gpu.Count -eq 0) { 'none recorded' } else {
    ($gpu | ForEach-Object { "{0} pid={1} project={2} max gpu={3}% ({4} probes)" -f $_.name, $_.pid, $_.project, (Format-Num $_.max_gpu_percent '0.0'), $_.probes }) -join ', '
})
Write-Output "- GPU-heavy across busy probes: $gpuText"
exit 0
