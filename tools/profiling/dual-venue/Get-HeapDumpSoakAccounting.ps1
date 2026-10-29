# Get-HeapDumpSoakAccounting.ps1 -- CPU-SCOPE-LIVE-HEAP-CORRUPTION-1 r7 (CoS #432, hub ruling 2026-10-15T11:25:44Z row a).
# Reads the per-leg records a heap-dump soak driver appends (one JSON object per line: arm, name, status, stopCycle, crash, dumps, and
# either captureState / watch / pageHeap (r7 drivers) or the r6b heapDumpWatch log string) and counts them in SEPARATE groups that are
# never pooled:
#   attached            the debugger watch was asked for and verified: captureState ATTACHED_NO_CRASH or DUMPED, or (an r6b record with
#                       no captureState) its watcher log shows DEBUGGER_ATTACHED;
#   unattached          no debugger watch: an r7 record with watch 'none', or an older record that carries no watch at all (r2 / r3);
#   pageheap            a full-page-heap leg (pageHeap true) whose capture is verified or that ran without the debugger by design;
#   captureNotVerified  the watch was asked for and is not verified ('capture not verified': NO_TARGET, KILLED, STOP_FAILED, or an r6b log
#                       with no DEBUGGER_ATTACHED line -- e.g. the procdump-shim legs, whose ATTACH line names a target that was never attached).
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

function Get-SoakCaptureClass($r) {
    $p = $r.PSObject.Properties
    $pageHeap = ($null -ne $p['pageHeap']) -and [bool]$r.pageHeap
    $watchNone = ($null -ne $p['watch']) -and ([string]$r.watch -ceq 'none')
    if ($null -ne $p['captureState'] -and -not $watchNone) {
        $verified = ([string]$r.captureState -cin @('ATTACHED_NO_CRASH', 'DUMPED'))
        if (-not $verified) { return 'captureNotVerified' }
        return $(if ($pageHeap) { 'pageheap' } else { 'attached' })
    }
    if ($watchNone) { return $(if ($pageHeap) { 'pageheap' } else { 'unattached' }) }
    if ($null -ne $p['heapDumpWatch']) {
        return $(if ([string]$r.heapDumpWatch -match 'DEBUGGER_ATTACHED pid=\d+') { 'attached' } else { 'captureNotVerified' })
    }
    return $(if ($pageHeap) { 'pageheap' } else { 'unattached' })
}

function Get-BinomialCdf([int]$n, [int]$k, [double]$p) {
    # P(X <= k) for X ~ Binomial(n, p)
    if ($n -le 0) { return 1.0 }
    $sum = 0.0
    $term = [math]::Pow(1.0 - $p, $n)
    for ($i = 0; $i -le $k -and $i -le $n; $i++) {
        $sum += $term
        if ($p -ge 1.0) { break }
        $term = $term * (($n - $i) / ($i + 1.0)) * ($p / (1.0 - $p))
    }
    [math]::Min(1.0, $sum)
}

$groupNames = @('attached', 'unattached', 'pageheap', 'captureNotVerified')
$groups = [ordered]@{}
foreach ($g in $groupNames) { $groups[$g] = [ordered]@{ legs = 0; stopCycles = 0; crashes = 0; dumps = 0; arms = [ordered]@{}; names = @() } }
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
        if ($null -ne $r.PSObject.Properties['stopCycle'] -and [bool]$r.stopCycle) { $g.stopCycles++ }
        $crash = ($null -ne $r.PSObject.Properties['crash']) -and [bool]$r.crash
        if ($crash) { $g.crashes++ }
        if ($null -ne $r.PSObject.Properties['dumps'] -and @($r.dumps).Count -gt 0) { $g.dumps++ }
        $arm = [string]$r.arm
        if (-not $g.arms.Contains($arm)) { $g.arms[$arm] = [ordered]@{ legs = 0; crashes = 0 } }
        $g.arms[$arm].legs++
        if ($crash) { $g.arms[$arm].crashes++ }
        $g.names += $name
    }
}

$un = $groups['unattached']
$unRate = $(if ($un.stopCycles -gt 0) { $un.crashes / [double]$un.stopCycles } else { $null })
$comparisons = @()
foreach ($gn in 'attached', 'pageheap') {
    $g = $groups[$gn]
    if ($null -ne $unRate -and $g.stopCycles -gt 0) {
        $comparisons += [ordered]@{ group = $gn; stopCycles = $g.stopCycles; crashes = $g.crashes; unattachedRate = [math]::Round($unRate, 4)
            pAtMostThisManyAtUnattachedRate = [math]::Round((Get-BinomialCdf $g.stopCycles $g.crashes $unRate), 3) }
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
