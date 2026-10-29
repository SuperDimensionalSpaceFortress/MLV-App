# Get-HeapDumpSoakAccounting.ps1 -- CPU-SCOPE-LIVE-HEAP-CORRUPTION-1 r7 (CoS #432, hub ruling 2026-10-15T11:25:44Z row a).
# Reads the per-leg records a heap-dump soak driver appends (one JSON object per line: arm, name, status, stopCycle, crash, dumps, and
# either captureState / watch / pageHeap (r7 drivers) or the r6b heapDumpWatch log string) and counts them in SEPARATE groups that are
# never pooled:
#   attached            the debugger watch was asked for and verified: captureState ATTACHED_NO_CRASH or DUMPED, or (an r6b record with
#                       no captureState) its watcher log shows DEBUGGER_ATTACHED;
#   unattached          no debugger watch: an r7 record with watch 'none', or an older record that carries no watch at all (r2 / r3);
#   pageheap            a full-page-heap leg (pageHeap true) whose capture is verified or that ran without the debugger by design;
#   captureNotVerified  the watch was asked for and is not verified ('capture not verified': NO_TARGET, KILLED, STOP_FAILED, or an r6b log
#                       with no DEBUGGER_ATTACHED line -- e.g. the procdump-shim legs, whose ATTACH line names a target that was never attached --
#                       or, r8, a record that names a debugger watch (watch other than 'none', or heapCorruptionDump true) with neither).
# Only legs with status RAN are counted; the rest are listed as notRun. A debugger attached to the app changes what it sees: CrashForensics'
# SetUnhandledExceptionFilter minidump never runs under a debugger, and a heap fail-fast is a debugger-visible first-chance exception, so an
# attached rate is never compared as if it were the unattached one. The binomial line says how likely the attached count is AT the
# unattached rate; it is a probability, not a claim that the debugger suppresses (or causes) the crash.
# ASCII only. Never launches anything; reads files only.
param(
    [Parameter(Mandatory = $true)][string[]]$LegsJsonl,
    [switch]$AsJson
)
$ErrorActionPreference = 'Stop'

# r10 (adversary r10 A3): a flag is true only as a JSON boolean true; the string 'false', whitespace and {} are PowerShell-truthy and never switch a leg group, a stop cycle or a crash on
function Test-JsonTrue($v) { ($v -is [bool]) -and $v }

function Get-SoakCaptureClass($r) {
    $p = $r.PSObject.Properties
    $pageHeap = ($null -ne $p['pageHeap']) -and (Test-JsonTrue $r.pageHeap)
    $watchNone = ($null -ne $p['watch']) -and ([string]$r.watch -ceq 'none')
    # r11 (adversary r10 A6): a leg is unattached only when nothing in its record asks for or shows the debugger: no captureState, no r6b watcher log,
    # heapCorruptionDump not true and no watch but 'none'. A record that says watch 'none' AND carries any of those contradicts itself: not verified.
    $debuggerNamed = ($null -ne $p['captureState']) -or ($null -ne $p['heapDumpWatch']) -or
        (($null -ne $p['heapCorruptionDump']) -and (Test-JsonTrue $r.heapCorruptionDump)) -or (($null -ne $p['watch']) -and -not $watchNone)
    if ($watchNone -and $debuggerNamed) { return 'captureNotVerified' }
    if ($null -ne $p['captureState'] -and -not $watchNone) {
        $verified = ([string]$r.captureState -cin @('ATTACHED_NO_CRASH', 'DUMPED'))
        if (-not $verified) { return 'captureNotVerified' }
        return $(if ($pageHeap) { 'pageheap' } else { 'attached' })
    }
    if ($watchNone) { return $(if ($pageHeap) { 'pageheap' } else { 'unattached' }) }
    if ($null -ne $p['heapDumpWatch']) {
        # r8 (adversary A4): a verified legacy attach keeps a page-heap leg in pageheap, as the typed branch does
        # r10 (adversary r10 A2): the event is the token right after a log line's timestamp, never text inside another line (the WATCH line carries the exe path)
        $logLines = [regex]::Split([string]$r.heapDumpWatch, '\r?\n| \| ')
        if (@($logLines | Where-Object { $_ -cmatch '^\S+ DEBUGGER_ATTACHED pid=\d+(\s|$)' }).Count -eq 0) { return 'captureNotVerified' }
        return $(if ($pageHeap) { 'pageheap' } else { 'attached' })
    }
    # r8 (CoS Iverson #432 item 2): a record that names a debugger watch but carries no capture evidence is not verified, never unattached
    if (($null -ne $p['watch']) -or (($null -ne $p['heapCorruptionDump']) -and (Test-JsonTrue $r.heapCorruptionDump))) { return 'captureNotVerified' }
    return $(if ($pageHeap) { 'pageheap' } else { 'unattached' })
}

function Get-BinomialCdf([int]$n, [int]$k, [double]$p) {
    # P(X <= k) for X ~ Binomial(n, p)
    if ($k -lt 0) { return 0.0 }
    if ($n -le 0 -or $k -ge $n -or $p -le 0.0) { return 1.0 }
    # r8 (CoS Iverson #432 item 4): at p = 1 every trial succeeds, so P(X <= k < n) = 0 (the loop's (1-p)^n start is 0 there and its ratio divides by 0)
    if ($p -ge 1.0) { return 0.0 }
    # r8 (adversary A5): the terms are summed in log space, so (1-p)^n underflowing a double (0.5^2000) cannot zero the whole sum
    $logTerm = $n * [math]::Log(1.0 - $p)
    $logRatio = [math]::Log($p / (1.0 - $p))
    $logTerms = [Collections.Generic.List[double]]::new()
    for ($i = 0; $i -le $k; $i++) {
        $logTerms.Add($logTerm)
        $logTerm += [math]::Log(($n - $i) / ($i + 1.0)) + $logRatio
    }
    $max = [double]::NegativeInfinity
    foreach ($l in $logTerms) { if ($l -gt $max) { $max = $l } }
    $scaled = 0.0
    foreach ($l in $logTerms) { $scaled += [math]::Exp($l - $max) }
    [math]::Min(1.0, [math]::Exp($max + [math]::Log($scaled)))
}

$groupNames = @('attached', 'unattached', 'pageheap', 'captureNotVerified')
$groups = [ordered]@{}
# r8 (adversary A6): stopCycleCrashes counts the crashes of stop-cycle legs only; the binomial rate and comparisons use it (crashes stays the total)
foreach ($g in $groupNames) { $groups[$g] = [ordered]@{ legs = 0; stopCycles = 0; crashes = 0; stopCycleCrashes = 0; dumps = 0; arms = [ordered]@{}; names = @() } }
$notRun = @()
foreach ($file in $LegsJsonl) {
    $source = Split-Path -Leaf $file
    foreach ($line in [IO.File]::ReadAllLines((Resolve-Path -LiteralPath $file).Path)) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $r = $line | ConvertFrom-Json
        $name = "$([string]$r.name) ($source)"
        if ([string]$r.status -cne 'RAN') { $notRun += $name; continue }
        $g = $groups[(Get-SoakCaptureClass $r)]
        $g.legs++
        $stopCycle = ($null -ne $r.PSObject.Properties['stopCycle']) -and (Test-JsonTrue $r.stopCycle)
        if ($stopCycle) { $g.stopCycles++ }
        $crash = ($null -ne $r.PSObject.Properties['crash']) -and (Test-JsonTrue $r.crash)
        if ($crash) { $g.crashes++ }
        if ($crash -and $stopCycle) { $g.stopCycleCrashes++ }
        # r10 (adversary r10 A4): a dump is an entry that names a file; null, "" and {} entries are not dumps
        if ($null -ne $r.PSObject.Properties['dumps'] -and @(@($r.dumps) | Where-Object { $null -ne $_ -and $null -ne $_.PSObject.Properties['name'] -and -not [string]::IsNullOrWhiteSpace([string]$_.name) }).Count -gt 0) { $g.dumps++ }
        $arm = [string]$r.arm
        if (-not $g.arms.Contains($arm)) { $g.arms[$arm] = [ordered]@{ legs = 0; crashes = 0 } }
        $g.arms[$arm].legs++
        if ($crash) { $g.arms[$arm].crashes++ }
        $g.names += $name
    }
}

$un = $groups['unattached']
$unRate = $(if ($un.stopCycles -gt 0) { $un.stopCycleCrashes / [double]$un.stopCycles } else { $null })
$comparisons = @()
foreach ($gn in 'attached', 'pageheap') {
    $g = $groups[$gn]
    if ($null -ne $unRate -and $g.stopCycles -gt 0) {
        $comparisons += [ordered]@{ group = $gn; stopCycles = $g.stopCycles; crashes = $g.stopCycleCrashes; unattachedRate = [math]::Round($unRate, 4)
            pAtMostThisManyAtUnattachedRate = [math]::Round((Get-BinomialCdf $g.stopCycles $g.stopCycleCrashes $unRate), 3) }
    }
}
$result = [ordered]@{ schema = 'mlv-app/heap-dump-soak-accounting/v1'; sources = @($LegsJsonl | ForEach-Object { Split-Path -Leaf $_ }); groups = $groups; notRun = $notRun; comparisons = $comparisons }

if ($AsJson) { $result | ConvertTo-Json -Depth 6; return }
foreach ($gn in $groupNames) {
    $g = $groups[$gn]
    $label = $(if ($gn -ceq 'captureNotVerified') { 'capture not verified' } else { $gn })
    $arms = ($g.arms.Keys | ForEach-Object { "$_=$($g.arms[$_].crashes)/$($g.arms[$_].legs)" }) -join ' '
    Write-Output ("{0}: legs={1} stopCycles={2} crashes={3} dumps={4} arms[{5}]" -f $label, $g.legs, $g.stopCycles, $g.crashes, $g.dumps, $arms)
    if ($gn -ceq 'captureNotVerified' -and $g.legs -gt 0) { Write-Output ('  capture not verified: ' + ($g.names -join ', ')) }
}
if ($notRun.Count -gt 0) { Write-Output ('not run: ' + ($notRun -join ', ')) }
foreach ($c in $comparisons) {
    Write-Output ("{0} vs unattached: {1} crash(es) in {2} stop cycles; at the unattached rate {3} P(<= {1}) = {4} (a probability, not a mechanism)" -f $c.group, $c.crashes, $c.stopCycles, $c.unattachedRate, $c.pAtMostThisManyAtUnattachedRate)
}
Write-Output 'Groups are never pooled: an attached debugger silences CrashForensics'' SetUnhandledExceptionFilter minidump and sees a heap fail-fast first-chance.'
