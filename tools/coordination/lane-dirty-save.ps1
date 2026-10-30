# lane-dirty-save.ps1 - LANE-DIRTY-SAVE-CORE-1 (core split out of parked PR #698).
# A dead lane's uncommitted work is the one thing a refusal or a clean-up destroys. When the lane process is dead, this writes into -RunDir:
#   dirty-save-<head8>.patch       git diff HEAD --binary, byte for byte (staged and unstaged tracked changes)
#   dirty-save-<head8>.untracked.txt  git ls-files --others --exclude-standard (only when something is untracked); .receipt.json = one JSON line, also printed
# The lane is named by -LanePid or by -ChildJson ({pid, createdUtc}; a process that started after createdUtc is a reused pid, so the lane is dead).
# Exit: 0 saved (or already saved with these exact bytes), 10 nothing to save, 20 lane alive (refused, nothing written), 30 bad arguments, 40 git failed.
# Never stashes, resets, checks out, cleans or deletes; an existing save with different bytes is kept (the new save gets a .2, .3 ... suffix).
# Every git call gets GIT_TRACE*=0 in its OWN environment (never the caller's): a trace variable or Trace2 config naming a tracked file would make git
# append to it (sol r20 B1 on #698), and removing a caller's disable is what exposed it. GIT_OPTIONAL_LOCKS=0 keeps git from refreshing the index.
param(
    [Parameter(Mandatory)][string]$WorkDir,
    [Parameter(Mandatory)][string]$RunDir,
    [int]$LanePid = 0,
    [string]$ChildJson = ''
)
$ErrorActionPreference = 'Stop'
function Invoke-GitBytes([string[]]$GitArgs) {
    $psi = [System.Diagnostics.ProcessStartInfo]::new('git')
    foreach ($a in (@('-C', $WorkDir) + $GitArgs)) { $psi.ArgumentList.Add($a) }
    $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true; $psi.UseShellExecute = $false
    foreach ($k in 'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE') { [void]$psi.Environment.Remove($k) }
    foreach ($k in (@('GIT_TRACE', 'GIT_TRACE2', 'GIT_TRACE2_EVENT', 'GIT_TRACE2_PERF') + @($psi.Environment.Keys | Where-Object { $_ -like 'GIT_TRACE*' }))) { $psi.Environment[$k] = '0' }
    $psi.Environment['GIT_OPTIONAL_LOCKS'] = '0'
    $p = [System.Diagnostics.Process]::Start($psi)
    $err = $p.StandardError.ReadToEndAsync()
    $ms = [System.IO.MemoryStream]::new()
    $p.StandardOutput.BaseStream.CopyTo($ms)
    $p.WaitForExit()
    return @{ Code = $p.ExitCode; Bytes = $ms.ToArray(); Err = $err.Result }
}
function Test-LaneAlive {
    $procId = $LanePid; $born = $null
    if ($ChildJson) {
        $raw = Get-Content -LiteralPath $ChildJson -Raw
        if (-not $procId) { $procId = [int]($raw | ConvertFrom-Json).pid }
        $m = [regex]::Match($raw, '"createdUtc"\s*:\s*"([^"]+)"')
        if ($m.Success) { $born = [datetimeoffset]::Parse($m.Groups[1].Value, [cultureinfo]::InvariantCulture, 'AssumeUniversal') }
    }
    if ($procId -le 0) { throw 'no-lane-pid' }
    $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if (-not $proc) { return $false }
    return -not ($born -and [datetimeoffset]$proc.StartTime.ToUniversalTime() -gt $born.AddSeconds(5))
}
try {
    if (-not (Test-Path -LiteralPath $WorkDir -PathType Container) -or -not (Test-Path -LiteralPath $RunDir -PathType Container)) { throw 'no-such-directory' }
    if (Test-LaneAlive) { Write-Output 'DIRTY_SAVE_REFUSED reason=lane-alive'; exit 20 }
} catch { Write-Output "DIRTY_SAVE_BAD_ARGS reason=$($_.Exception.Message)"; exit 30 }
$head = Invoke-GitBytes @('rev-parse', 'HEAD')
$patch = Invoke-GitBytes @('diff', 'HEAD', '--binary', '--no-color', '--no-ext-diff', '--no-textconv', '--src-prefix=a/', '--dst-prefix=b/')
$untracked = Invoke-GitBytes @('ls-files', '--others', '--exclude-standard')
$bad = @($head, $patch, $untracked) | Where-Object { $_.Code -ne 0 } | Select-Object -First 1
if ($bad) { Write-Output "DIRTY_SAVE_GIT_FAILED reason=$($bad.Err.Trim())"; exit 40 }
if ($patch.Bytes.Length -eq 0 -and $untracked.Bytes.Length -eq 0) { Write-Output 'DIRTY_SAVE_NOTHING reason=clean-tree'; exit 10 }

$head8 = [System.Text.Encoding]::ASCII.GetString($head.Bytes).Trim().Substring(0, 8)
$wanted = @(@{ Ext = 'patch'; Bytes = $patch.Bytes }, @{ Ext = 'untracked.txt'; Bytes = $untracked.Bytes }) | Where-Object { $_.Bytes.Length -gt 0 }
for ($n = 1; ; $n++) {
    $stem = if ($n -eq 1) { "dirty-save-$head8" } else { "dirty-save-$head8.$n" }
    $clash = $wanted | Where-Object { $f = Join-Path $RunDir "$stem.$($_.Ext)"; (Test-Path -LiteralPath $f) -and -not [System.Linq.Enumerable]::SequenceEqual([byte[]][System.IO.File]::ReadAllBytes($f), [byte[]]$_.Bytes) }
    if (-not $clash) { break }
}
foreach ($w in $wanted) {
    $f = Join-Path $RunDir "$stem.$($w.Ext)"
    if (-not (Test-Path -LiteralPath $f)) { $fs = [System.IO.File]::Open($f, 'CreateNew', 'Write', 'None'); try { $fs.Write($w.Bytes, 0, $w.Bytes.Length) } finally { $fs.Dispose() } }
}
$receipt = [ordered]@{
    schema = 'lane-dirty-save/v1'; head = [System.Text.Encoding]::ASCII.GetString($head.Bytes).Trim(); utc = [datetime]::UtcNow.ToString('o')
    patch = if ($patch.Bytes.Length) { "$stem.patch" } else { $null }; patchBytes = $patch.Bytes.Length
    patchSha256 = [System.BitConverter]::ToString([System.Security.Cryptography.SHA256]::HashData($patch.Bytes)).Replace('-', '').ToLowerInvariant()
    untracked = if ($untracked.Bytes.Length) { "$stem.untracked.txt" } else { $null }
} | ConvertTo-Json -Compress
$rf = Join-Path $RunDir "$stem.receipt.json"
if (-not (Test-Path -LiteralPath $rf)) { [System.IO.File]::WriteAllText($rf, $receipt + "`n", [System.Text.UTF8Encoding]::new($false)) }
Write-Output $receipt
exit 0
